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


def _stamped(records: list[dict], t: datetime) -> list[dict]:
    return [{"timestamp": _iso(t), **r} for r in records]


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
        # Real order: the model answers the last input (its latency), calls a tool, the tool runs, and the response's
        # token_count is written after the tool's output. An idle gap is spent while that tool output is pending, so
        # each step writes the previous step's output and token_count after its gap.
        pending: list[dict] = []
        for k, (ctx, gap, key, utext, compacted, pre) in enumerate(_steps(rng, rng.randint(80, 160), 20_000, 300_000)):
            t += timedelta(seconds=gap)
            lines += _stamped(pending, t)
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
            t += _lat(rng, ctx)
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
            output = {"type": "function_call_output", "call_id": f"c{k}", "output": "y" * rng.randint(300, 4000)}
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
            ev = {"type": "event_msg", "payload": {"type": "token_count", "info": info}}
            pending = [{"type": "response_item", "payload": output}, ev, ev]  # Codex re-emits identical token_counts
            t += timedelta(milliseconds=500)  # the tool runs
        lines += _stamped(pending, t)
        path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        out.append(path)
    return out


def write_dsh(root: Path, n: int = 4, seed: int = 13, model: str = "deepseek-v4-flash") -> list[Path]:
    """DeepSeek Harness logs as plain JSONL (dsh writes this with persistenceCompression 'none')."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        sdir = root / "--proj--" / f"dsh-{i}"
        sdir.mkdir(parents=True, exist_ok=True)
        path = sdir / "session.v4.jsonl"
        t = EPOCH + timedelta(days=i)
        seq = 0

        def ev(typ: str, data: dict[str, Any], when: datetime) -> dict[str, Any]:
            nonlocal seq
            seq += 1
            return {"type": typ, "seq": seq - 1, "time": int(when.timestamp() * 1000), "data": data}

        lines: list[dict[str, Any]] = [
            {
                "type": "session",
                "version": 4,
                "id": f"dsh-{i}",
                "createdAt": int(t.timestamp() * 1000),
                "cwd": "/proj",
                "isSeeded": False,
                "delegationDepth": 0,
            },
            ev("request/context", {"provider": "deepseek-official", "model": model, "contextWindow": 1_048_576}, t),
        ]
        steps = _steps(rng, rng.randint(80, 160), 20_000, 600_000)
        for k, (ctx, gap, key, utext, compacted, pre) in enumerate(steps):
            t += timedelta(seconds=gap)
            if compacted:
                lines.append(ev("compaction/start", {"compactionId": f"c{k}", "turn": k}, t))
                t += _clat(rng, pre)
                summary = {"compactionId": f"c{k}", "summary": "Summary of work so far.\n" + SUMMARY_FILLER,
                           "shadowedTokenCount": pre, "provider": "deepseek-official", "model": model}  # fmt: skip
                lines += [ev("compaction/summary", summary, t), ev("compaction/end", {"compactionId": f"c{k}"}, t)]
            if utext:
                lines.append(ev("user/message", {"content": [{"type": "text", "text": utext}]}, t))
            body = "y" * rng.randint(300, 4000)
            lines.append(ev("tool/result", {"message": {"role": "tool", "toolCallId": f"t{k - 1}",
                                                        "content": [{"type": "text", "text": body}]}}, t))  # fmt: skip
            t += _lat(rng, ctx)
            hit = int(ctx * 0.85)
            usage = {"inputTokens": ctx - hit, "cacheReadTokens": hit, "outputTokens": rng.randint(50, 600)}
            msg = {"role": "assistant", "source": {"kind": "model", "provider": "deepseek-official", "model": model}}
            lines.append(ev("assistant/message", {"turn": k, "step": 1, "message": msg, "usage": usage}, t))
            lines.append(
                ev("tool/call", {"callId": f"t{k}", "name": "read", "arguments": json.dumps({"file_path": key})}, t)
            )
        path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        out.append(path)
    return out


OPENCODE_SCHEMA = """
create table session (id text primary key, project_id text not null, parent_id text, time_created integer not null,
                      time_updated integer not null);
create table message (id text primary key, session_id text not null, time_created integer not null,
                      time_updated integer not null, data text not null);
create table part (id text primary key, message_id text not null, session_id text not null,
                   time_created integer not null, time_updated integer not null, data text not null);
"""


def write_opencode(root: Path, n: int = 4, seed: int = 17, model: str = "anthropic/claude-sonnet-5-5") -> Path:
    """An OpenCode 1.x database (only the tables and columns compactlab reads) with n main sessions, each with one
    subagent session, steps with reasoning and a tool, and compactions past 600k."""
    import sqlite3

    rng = random.Random(seed)
    provider, model_id = model.split("/", 1)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "opencode.db"
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    con.executescript(OPENCODE_SCHEMA)
    ids = {"m": 0, "p": 0}

    def ms(when: datetime) -> int:
        return int(when.timestamp() * 1000)

    def msg(sid: str, data: dict[str, Any], when: datetime) -> str:
        ids["m"] += 1
        mid = f"msg_{ids['m']:08d}"
        con.execute("insert into message values (?,?,?,?,?)", (mid, sid, ms(when), ms(when), json.dumps(data)))
        return mid

    def part(sid: str, mid: str, data: dict[str, Any], when: datetime) -> None:
        ids["p"] += 1
        con.execute("insert into part values (?,?,?,?,?,?)",
                    (f"prt_{ids['p']:08d}", mid, sid, ms(when), ms(when), json.dumps(data)))  # fmt: skip

    def session(sid: str, parent: str | None, t: datetime, n_steps: int, limit: int | None) -> datetime:
        start = t
        user = {"role": "user", "time": {"created": ms(t)}, "agent": "build",
                "model": {"providerID": provider, "modelID": model_id}}  # fmt: skip
        mid = msg(sid, user, t)
        part(sid, mid, {"type": "text", "text": "refactor the module"}, t)
        for k, (ctx, gap, key, utext, compacted, pre) in enumerate(_steps(rng, n_steps, 20_000, limit)):
            t += timedelta(seconds=gap)
            if compacted:
                cu = msg(sid, user | {"time": {"created": ms(t)}}, t)
                part(sid, cu, {"type": "compaction", "auto": True}, t)
                done = t + _clat(rng, pre)
                stok = {"input": pre, "output": 3000, "reasoning": 0, "cache": {"read": 0, "write": 0}}
                summ = {"role": "assistant", "parentID": cu, "mode": "compaction", "agent": "compaction",
                        "summary": True, "finish": "stop", "providerID": provider, "modelID": model_id,
                        "time": {"created": ms(t), "completed": ms(done)}, "tokens": stok}  # fmt: skip
                sm = msg(sid, summ, t)
                part(sid, sm, {"type": "step-start"}, t)
                part(sid, sm, {"type": "text", "text": "## Objective\n" + SUMMARY_FILLER[:4000]}, done)
                part(sid, sm, {"type": "step-finish", "reason": "stop", "cost": 0, "tokens": stok}, done)
                t = done
            if utext:
                um = msg(sid, user | {"time": {"created": ms(t)}}, t)
                part(sid, um, {"type": "text", "text": utext}, t)
            lat = _lat(rng, ctx)
            hit = int(ctx * 0.85)
            tokens = {"input": ctx - hit - 500, "output": rng.randint(50, 600), "reasoning": rng.randint(0, 200),
                      "cache": {"read": hit, "write": 500}}  # fmt: skip
            am = {"role": "assistant", "parentID": mid, "mode": "build", "agent": "build", "finish": "tool-calls",
                  "providerID": provider, "modelID": model_id, "tokens": tokens,
                  "time": {"created": ms(t), "completed": ms(t + lat + timedelta(seconds=2))}}  # fmt: skip
            a = msg(sid, am, t)
            part(sid, a, {"type": "step-start"}, t)
            part(sid, a, {"type": "reasoning", "text": "thinking"}, t)
            state = {"status": "completed", "input": {"filePath": key}, "output": "y" * rng.randint(300, 4000),
                     "time": {"start": ms(t + lat), "end": ms(t + lat + timedelta(seconds=1))}}  # fmt: skip
            part(sid, a, {"type": "tool", "tool": "read", "callID": f"c{k}", "state": state}, t + lat)
            t += lat + timedelta(seconds=2)
            part(sid, a, {"type": "step-finish", "reason": "tool-calls", "cost": 0.01, "tokens": tokens}, t)
        con.execute("insert into session values (?,?,?,?,?)", (sid, "proj", parent, ms(start), ms(t)))
        return t

    for i in range(n):
        t = session(f"ses_main{i}", None, EPOCH + timedelta(days=i), rng.randint(80, 160), 600_000)
        session(f"ses_sub{i}", f"ses_main{i}", t, rng.randint(10, 30), None)
    con.commit()
    con.close()
    return path


def write_gemini(root: Path, n: int = 3, seed: int = 19, model: str = "gemini-3-pro-preview") -> list[Path]:
    """Gemini CLI JSONL chats under <root>/proj/chats: re-written messages, tool results as user messages, a subagent,
    and compressions past 600k (the first session in the 0.46 checkpoint form, the others in the 0.70 patch form)."""
    rng = random.Random(seed)
    chats = root / "proj" / "chats"
    chats.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        t = EPOCH + timedelta(days=i)
        sid = f"0000000{i}-aaaa-bbbb-cccc-dddddddddddd"
        lines: list[dict[str, Any]] = [
            {"sessionId": sid, "projectHash": "0" * 64, "startTime": _iso(t), "lastUpdated": _iso(t)},
            {"id": f"u{i}-0", "timestamp": _iso(t), "type": "user", "content": [{"text": "add a test"}]},
        ]
        for k, (ctx, gap, key, utext, compacted, pre) in enumerate(_steps(rng, rng.randint(60, 120), 20_000, 600_000)):
            t += timedelta(seconds=gap)
            if compacted:
                t += _clat(rng, pre)
                snap = {
                    "id": f"s{i}-{k}",
                    "timestamp": _iso(t),
                    "type": "user",
                    "content": [{"text": "<state_snapshot>\n" + SUMMARY_FILLER[:4000] + "\n</state_snapshot>"}],
                }
                ack = {"id": f"a{i}-{k}", "timestamp": _iso(t), "type": "gemini",
                       "content": [{"text": "Got it. Thanks for the additional context!"}]}  # fmt: skip
                if i == 0:
                    lines += [{"$set": {"sessionId": sid}}, {"$set": {"messages": [snap, ack], "lastUpdated": _iso(t)}}]
                else:
                    patch = {"removeIds": [f"g{i}-{k - 1}"], "orderIds": [snap["id"], ack["id"]]}
                    lines += [snap, ack, {"$patch": patch}]
            if utext:
                lines.append({"id": f"u{i}-{k}", "timestamp": _iso(t), "type": "user", "content": [{"text": utext}]})
            t += _lat(rng, ctx)
            base = {"id": f"g{i}-{k}", "timestamp": _iso(t), "type": "gemini", "content": "", "model": model}
            tokens = {"input": ctx, "output": rng.randint(50, 600), "cached": int(ctx * 0.8),
                      "thoughts": rng.randint(0, 300), "tool": 0}  # fmt: skip
            tokens["total"] = tokens["input"] + tokens["output"] + tokens["thoughts"]
            call = {
                "id": f"read_file_{k}",
                "name": "read_file",
                "args": {"file_path": key},
                "status": "success",
                "timestamp": _iso(t),
                "result": [{"functionResponse": {"response": {"output": "y" * 900}}}],
            }
            lines += [base, base | {"tokens": tokens}, base | {"tokens": tokens, "toolCalls": [call]}]
            lines.append({"id": f"r{i}-{k}", "timestamp": _iso(t), "type": "user",
                          "content": [{"functionResponse": {"id": call["id"], "name": "read_file"}}]})  # fmt: skip
            lines.append({"$set": {"lastUpdated": _iso(t)}})
        path = chats / f"session-{EPOCH + timedelta(days=i):%Y-%m-%dT%H-%M}-{sid[:8]}.jsonl"
        path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        out.append(path)
    sub = chats / "00000000-aaaa-bbbb-cccc-dddddddddddd" / "sub-0001.jsonl"
    sub.parent.mkdir(exist_ok=True)
    t = EPOCH
    rows: list[dict[str, Any]] = [{"sessionId": "sub-0001", "projectHash": "0" * 64, "startTime": _iso(t),
                                   "lastUpdated": _iso(t), "kind": "subagent"}]  # fmt: skip
    for k in range(8):
        t += timedelta(seconds=20)
        rows.append({"id": f"sg{k}", "timestamp": _iso(t), "type": "gemini", "model": model,
                     "tokens": {"input": 30_000 + k * 4000, "output": 200, "cached": 20_000, "total": 0}})  # fmt: skip
    sub.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
    out.append(sub)
    return out
