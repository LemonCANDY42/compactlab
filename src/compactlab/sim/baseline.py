"""The harness's default compaction point and the user's currently configured one."""

from __future__ import annotations

import json
import os
import re
from importlib import resources
from pathlib import Path
from typing import Any

_DEFAULTS: dict[str, Any] | None = None


def defaults() -> dict[str, Any]:
    global _DEFAULTS
    if _DEFAULTS is None:
        _DEFAULTS = json.loads(resources.files("compactlab").joinpath("data/harness_defaults.json").read_text())
    return _DEFAULTS


def default_threshold(harness: str, model: str, window: int) -> tuple[int, str]:
    """(threshold, explanation). Never above the window. See default_note_zh() for the Chinese note."""
    d = defaults().get(harness)
    m = model.lower()
    if harness == "claude-code" and d:
        for r in d["rules"]:
            if any(p in m for p in r["match"]):
                return min(int(r["threshold"]), window), r["note"]
        return window, d["otherwise_note"]
    if harness == "codex" and d:
        return int(window * d["fraction"]), d["note"]
    if harness == "dsh" and d:
        return dsh_trigger(window, d["threshold_ratio"], d["headroom_tokens"]), d["note"]
    return window, "unknown harness: assuming compaction at the context window"


def dsh_trigger(window: int, ratio: float, headroom: int) -> int:
    """dsh-compaction-basic pressure point: min(window x ratio, window - reserved output - headroom).
    The reserve is the route's max output (256k for DeepSeek's own API). A smaller window cannot fit that, so for
    windows up to twice the reserve the route's own cap is assumed to be a quarter of the window."""
    reserved = int(defaults()["dsh"]["reserved_output"])
    if window <= 2 * reserved:
        reserved = window // 4
    return max(int(min(window * ratio, window - reserved - headroom)), 0)


def effective_max(harness: str, window: int) -> int:
    """Largest threshold the harness will actually honour."""
    if harness == "codex":
        return int(window * defaults()["codex"]["fraction"])
    if harness == "dsh":
        return dsh_trigger(window, 1.0, 0)  # thresholdRatio 1 with headroomTokens 0: only the output reserve remains
    return window


def read_current(harness: str, model: str, path: Path | None) -> tuple[int | None, str]:
    """Currently configured threshold for `model` from one config file, read-only.
    Returns (value or None for 'not set / auto', description)."""
    if harness == "dsh":
        return None, "not read: where dsh keeps the compaction-basic plugin config is not verified"
    if path is None or not path.exists():
        return None, "no config file"
    text = path.read_text()
    if harness == "claude-code":
        try:
            d = json.loads(text) if text.strip() else {}
        except ValueError:
            return None, f"{path.name} is not strict JSON"
        ms = (d.get("modelSettings") or {}).get(model) or {}
        v = ms.get("autoCompactWindow") if isinstance(ms, dict) else None
        src = f"modelSettings.{model}.autoCompactWindow"
        if v is None:
            v, src = d.get("autoCompactWindow"), "autoCompactWindow"
        if v is None or v == "auto":
            return None, "not set (harness default)"
        return int(v), f"{src} in {path.name}"
    if harness == "codex":
        top = text[: m.start()] if (m := re.search(r"^[ \t]*\[", text, re.M)) else text
        mm = re.search(r"^[ \t]*model_auto_compact_token_limit[ \t]*=[ \t]*([\d_]+)", top, re.M)
        if not mm:
            return None, "not set (harness default)"
        return int(mm.group(1).replace("_", "")), f"model_auto_compact_token_limit in {path.name}"
    return None, "unsupported harness"


IDLE_ENV = "CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS"


def read_idle(path: Path | None, environ: dict[str, str] | None = None) -> tuple[bool | None, int | None, str]:
    """Claude Code's idle compaction as configured, read-only: (`idleCompaction` or None when not set, the minimum
    context from CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS or None, description). The env var counts from settings.json's
    `env` block first, then this process's environment."""
    d: dict = {}
    if path is not None and path.exists():
        try:
            text = path.read_text()
            d = json.loads(text) if text.strip() else {}
        except (OSError, ValueError):
            return None, None, f"{path.name} unreadable: idle compaction assumed on"
    d = d if isinstance(d, dict) else {}
    v = d.get("idleCompaction")
    enabled = v if isinstance(v, bool) else None
    env = d.get("env")
    env = env if isinstance(env, dict) else {}
    raw = env.get(IDLE_ENV) or (environ if environ is not None else os.environ).get(IDLE_ENV)
    try:
        min_tokens = int(str(raw).replace("_", "")) if raw else None
    except ValueError:
        min_tokens = None
    where = f"idleCompaction in {path.name}" if enabled is not None and path else "not set (on by default)"
    return enabled, min_tokens, where + (f"; {IDLE_ENV}={min_tokens:,}" if min_tokens else "")


def default_note_zh(harness: str, model: str) -> str:
    d = defaults().get(harness) or {}
    m = model.lower()
    if harness == "claude-code" and d:
        for r in d["rules"]:
            if any(p in m for p in r["match"]):
                return str(r.get("note_zh", r["note"]))
        return str(d.get("otherwise_note_zh", d.get("otherwise_note", "")))
    return str(d.get("note_zh", d.get("note", "")))
