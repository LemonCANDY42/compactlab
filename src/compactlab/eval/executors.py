"""LLM clients that run through a local agent harness (for deep mode).

They shell out to the harness CLI in an empty temporary directory, send the prompt on stdin, and parse
the reply. Flags are the documented non-interactive modes; check them against your installed version
(`claude --help`, `codex exec --help`). Token counts come from the CLI's JSON when present, else are
estimated from text length (marked `estimated`).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .deep import Completion, EvalError

Runner = Callable[..., str]  # (cmd, prompt, cwd) -> stdout; also takes env= when the executor sets one


def _run(cmd: Sequence[str], prompt: str, cwd: str, env: Mapping[str, str] | None = None) -> str:
    try:
        r = subprocess.run(
            list(cmd),
            input=prompt,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=600,
            env={**os.environ, **env} if env else None,
        )
    except FileNotFoundError as e:
        raise EvalError(f"executor not found: {cmd[0]} (is it installed and on PATH?)") from e
    except subprocess.TimeoutExpired as e:
        raise EvalError(f"executor timed out: {cmd[0]}") from e
    if r.returncode != 0:
        raise EvalError(f"{cmd[0]} exited {r.returncode}: {r.stderr.strip()[:300]}")
    return r.stdout


class ClaudeCodeExecutor:
    """`claude -p --model M --output-format json` (print mode, no interactive session)."""

    def __init__(self, runner: Runner = _run, binary: str = "claude"):
        self.runner, self.binary = runner, binary

    def complete(self, model: str, prompt: str, max_tokens: int) -> Completion:
        with tempfile.TemporaryDirectory(prefix="compactlab-deep-") as d:
            out = self.runner(
                [self.binary, "-p", "--model", model, "--output-format", "json"],
                prompt,
                d,
                env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max(int(max_tokens), 1))},
            )
        try:
            j: dict[str, Any] = json.loads(out)
        except ValueError:
            return Completion(out.strip(), len(prompt) // 4 + 1, len(out) // 4 + 1)
        u = j.get("usage") or {}
        inp = (
            int(u.get("input_tokens") or 0)
            + int(u.get("cache_read_input_tokens") or 0)
            + int(u.get("cache_creation_input_tokens") or 0)
        )
        text = str(j.get("result", ""))
        return Completion(text, inp or len(prompt) // 4 + 1, int(u.get("output_tokens") or len(text) // 4 + 1))


class CodexExecutor:
    """`codex exec -m M -` (non-interactive; prompt on stdin). Token usage is estimated."""

    def __init__(self, runner: Runner = _run, binary: str = "codex"):
        self.runner, self.binary = runner, binary

    def complete(self, model: str, prompt: str, max_tokens: int) -> Completion:
        with tempfile.TemporaryDirectory(prefix="compactlab-deep-") as d:
            out = self.runner([self.binary, "exec", "-m", model, "--skip-git-repo-check", "-"], prompt, d)
        text = out.strip()
        return Completion(text, len(prompt) // 4 + 1, len(text) // 4 + 1)


EXECUTORS: dict[str, type] = {"claude-code": ClaudeCodeExecutor, "codex": CodexExecutor}
