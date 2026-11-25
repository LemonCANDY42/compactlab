"""What `compactlab engine ...` prints: the contract between the engine and the compactlab app (or any other tool).

Every JSON object the engine writes has `engine_api` (this contract's version) and `kind`. Within one version fields
and kinds are only added; a consumer ignores what it does not know. Removing, renaming or retyping a field bumps
ENGINE_API. Every kind has an example in tests/contract/ (the app's tests decode the same files), and
docs/ENGINE_API.md describes them.
"""

from __future__ import annotations

import json
import platform
from pathlib import Path
from typing import Any

from .. import __version__
from ..lens import summarize
from ..sim.select import DEFAULT_PROFILE

ENGINE_API = 1
COMMANDS = ("hello", "sessions", "watch", "result", "calibrate")
LIVE_HARNESSES = ("claude-code", "codex")  # harnesses `sessions` / `watch` read


def message(kind: str, **body: Any) -> dict[str, Any]:
    return {"engine_api": ENGINE_API, "kind": kind, **body}


def error(code: str, text: str) -> dict[str, Any]:
    """codes: no_result, bad_result, calibration_failed, busy, bad_command, scan_failed, internal"""
    return message("error", error={"code": code, "message": text})


def dumps(msg: dict[str, Any]) -> str:
    """One line of JSON (what `watch` writes per message; the one-shot commands print the same)."""
    return json.dumps(msg, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _read_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except (OSError, ValueError):
        return None


def result_path(state_dir: Path) -> Path:
    return state_dir / "service" / "result.json"


def service_state(state_dir: Path) -> dict[str, Any]:
    """The background calibrator's bookkeeping (`serve`, `recalibrate`, `engine calibrate` all keep it)."""
    st = _read_json(state_dir / "service" / "state.json")
    st = st if isinstance(st, dict) else {}
    return {
        "last_calibrated": st.get("last_ok"),
        "last_attempt": st.get("last_attempt"),
        "failures": int(st.get("failures") or 0),
        "last_error": st.get("last_error"),
    }


def hello(harnesses: list[dict[str, Any]], state_dir: Path) -> dict[str, Any]:
    res = result_path(state_dir)
    return message(
        "hello",
        version=__version__,
        python=platform.python_version(),
        commands=list(COMMANDS),
        live_harnesses=list(LIVE_HARNESSES),
        harnesses=harnesses,
        state_dir=str(state_dir),
        result={"path": str(res), "exists": res.exists()},
        service=service_state(state_dir),
    )


def result_message(
    res: dict[str, Any], state_dir: Path | None = None, auto_apply: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The last calibration for the app's threshold table: per group the pick of every profile and what it would
    change against the current setting and the harness default, plus the idle-compaction verdict, the Codex
    single-key decision and the model notes (unpriced, unset, window or default changes)."""
    lens = summarize(res)
    groups = [
        {
            **s,
            "platform": g.get("platform"),
            "current_note": g.get("current_note") or None,
            "window_source": g.get("window_source"),
            "n_sessions": g.get("n_sessions"),
            "n_turns": g.get("n_turns"),
            "superseded_by": g.get("superseded_by"),
        }
        for g, s in zip(res["groups"], lens["groups"], strict=True)
    ]
    return message(
        "result",
        calibration=res.get("calibration"),
        mode=res.get("mode"),
        data_through=res.get("data_through"),
        demo=bool(res.get("demo")),
        profile=(res.get("options") or {}).get("profile") or DEFAULT_PROFILE,
        harnesses=lens["harnesses"],  # per harness: the run its groups come from (the result keeps each one's latest)
        groups=groups,
        idle_compaction=res.get("idle_compaction"),
        global_keys=res.get("global_keys") or {},
        model_notes=res.get("model_notes") or {},
        service=service_state(state_dir) if state_dir else None,
        auto_apply=auto_apply,  # what `engine calibrate --auto-apply` decided; null otherwise
    )
