"""The harness's default compaction point and the user's currently configured one."""

from __future__ import annotations

import json
import os
import re
from importlib import resources
from pathlib import Path
from typing import Any

from ..pricing.ids import claude_settings_key, parse

_DEFAULTS: dict[str, Any] | None = None
OPENCODE_AUTO_OFF = "compaction.auto is false"  # read_current's note when OpenCode never compacts by itself
OPENCODE_OUTPUT_CAP = 32_000  # OpenCode's OUTPUT_TOKEN_MAX: the most output it reserves for any model


def defaults() -> dict[str, Any]:
    global _DEFAULTS
    if _DEFAULTS is None:
        _DEFAULTS = json.loads(resources.files("compactlab").joinpath("data/harness_defaults.json").read_text())
    return _DEFAULTS


def _claude_rule(d: dict[str, Any], model: str, window: int | None) -> dict[str, Any] | None:
    """The Claude Code default rule for `model`: by name, else by window (`min_window`: a model Claude Code knows with
    a native window that large, such as a Claude model newer than the list)."""
    m = model.lower()
    for r in d["rules"]:
        if any(p in m for p in r["match"]):
            return r
    if window and parse(model).id.startswith("claude-"):
        return next((r for r in d["rules"] if r.get("min_window") and window >= r["min_window"]), None)
    return None


def default_threshold(harness: str, model: str, window: int) -> tuple[int, str]:
    """(threshold, explanation). Never above the window. See default_note_zh() for the Chinese note."""
    d = defaults().get(harness)
    if harness == "claude-code" and d:
        r = _claude_rule(d, model, window)
        if r:
            return min(int(r["threshold"]), window), r["note"]
        return window, d["otherwise_note"]
    if harness == "codex" and d:
        return int(window * d["fraction"]), d["note"]
    if harness == "dsh" and d:
        return dsh_trigger(window, d["threshold_ratio"], d["headroom_tokens"]), d["note"]
    if harness == "opencode" and d:
        return max(window - int(d["output_reserve"]), 0), d["note"]
    if harness == "gemini-cli" and d:
        return int(window * d["fraction"]), d["note"]
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
    if harness == "opencode":
        return max(window - int(defaults()["opencode"]["reserved_default"]), 0)  # limit.input = window, reserved kept
    return window


def read_current(harness: str, model: str, path: Path | None) -> tuple[int | None, str]:
    """Currently configured threshold for `model` from one config file, read-only.
    Returns (value or None for 'not set / auto', description)."""
    if harness == "dsh":
        return None, "not read: where dsh keeps the compaction-basic plugin config is not verified"
    if path is None or not path.exists():
        return None, "no config file"
    text = path.read_text()
    if harness in ("opencode", "gemini-cli"):
        try:
            d = json.loads(text) if text.strip() else {}
        except ValueError:
            return None, f"{path.name} is not strict JSON (comments are not read)"
        return (_opencode_current if harness == "opencode" else _gemini_current)(
            d if isinstance(d, dict) else {}, model, path.name
        )
    if harness == "claude-code":
        try:
            d = json.loads(text) if text.strip() else {}
        except ValueError:
            return None, f"{path.name} is not strict JSON"
        table = d.get("modelSettings") or {}
        # keyed by canonical name, which also covers dated and Bedrock / Vertex IDs of the same model (not its [1m]
        # variant, which is a different model to Claude Code)
        canon = claude_settings_key(model)
        mid = parse(model)
        key = next(
            (k for k in (canon, model) if k in table),
            next((k for k in table if parse(k).id == mid.id and parse(k).one_m == mid.one_m), model),
        )
        ms = table.get(key) or {}
        v = ms.get("autoCompactWindow") if isinstance(ms, dict) else None
        src = f"modelSettings.{key}.autoCompactWindow"
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


def _obj(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _num(v: Any) -> int:
    return int(v) if isinstance(v, int | float) and not isinstance(v, bool) and v > 0 else 0


def _opencode_current(d: dict[str, Any], model: str, name: str) -> tuple[int | None, str]:
    """OpenCode 1.x: limit.input - compaction.reserved when the model's limit sets input, else context - output."""
    comp = _obj(d.get("compaction"))
    if comp.get("auto") is False:
        return None, f"{OPENCODE_AUTO_OFF} in {name}: OpenCode does not compact by itself"
    prov, _, mid = model.partition("/")
    lim = _obj(_obj(_obj(_obj(_obj(d.get("provider")).get(prov)).get("models")).get(mid)).get("limit"))
    if not _num(lim.get("context")):
        return None, "not set (harness default)"
    out = min(_num(lim.get("output")), OPENCODE_OUTPUT_CAP) or OPENCODE_OUTPUT_CAP
    key = f"provider.{prov}.models.{mid}.limit"
    if _num(lim.get("input")):
        res = comp.get("reserved")
        reserved = int(res) if isinstance(res, int) else min(int(defaults()["opencode"]["reserved_default"]), out)
        return max(_num(lim["input"]) - reserved, 0), f"{key}.input - reserved {reserved:,} in {name}"
    return max(_num(lim["context"]) - out, 0), f"{key}.context - output {out:,} in {name}"


def _gemini_current(d: dict[str, Any], model: str, name: str) -> tuple[int | None, str]:
    from ..adapters.gemini_cli import token_limit

    v = _obj(d.get("model")).get("compressionThreshold")
    if not isinstance(v, int | float) or isinstance(v, bool) or not 0 < v <= 1:
        return None, "not set (harness default)"
    return int(v * token_limit(model)), f"model.compressionThreshold {v:g} in {name}"


IDLE_ENV = "CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS"
MAX_CONTEXT_ENV = "CLAUDE_CODE_MAX_CONTEXT_TOKENS"


def read_env_int(path: Path | None, name: str, environ: dict[str, str] | None = None) -> int | None:
    """An integer Claude Code setting from settings.json's `env` block, else this process's environment."""
    d: Any = {}
    if path is not None and path.exists():
        try:
            text = path.read_text()
            d = json.loads(text) if text.strip() else {}
        except (OSError, ValueError):
            d = {}
    env = d.get("env") if isinstance(d, dict) else None
    env = env if isinstance(env, dict) else {}
    raw = env.get(name) or (environ if environ is not None else os.environ).get(name)
    try:
        return int(str(raw).replace("_", "")) if raw else None
    except ValueError:
        return None


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


def default_note_zh(harness: str, model: str, window: int | None = None) -> str:
    d = defaults().get(harness) or {}
    if harness == "claude-code" and d:
        r = _claude_rule(d, model, window)
        if r:
            return str(r.get("note_zh", r["note"]))
        return str(d.get("otherwise_note_zh", d.get("otherwise_note", "")))
    return str(d.get("note_zh", d.get("note", "")))
