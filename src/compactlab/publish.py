"""The shared files other programs read hold the latest run of every harness.

Two files are read by programs other than the run that wrote them: the lens summary (`--lens-out`, default
`~/.compactlab/lens.json`, read by the compactlab-lens mod) and the calibrator's result (`<state-dir>/service/
result.json`, read by `engine result`, `serve` and `top`). A run replaces the harnesses it covered and keeps the
entries of every other harness from the file, so `simulate --harness dsh` no longer leaves a lens with dsh alone.
Everything a result holds per harness is merged that way: groups, sessions, the single-key decision (`global_keys`),
Claude Code's idle-compaction verdict and the model notes.

`harnesses` says, per harness, which run its entries come from: mode, newest turn, calibration and, in a result, the
options its groups were selected with. Kept groups keep their picks; re-selection (`--profile`) and the threshold cap
use each harness's own options (`sim.engine.harness_options`), since another run's options may not even be feasible
for them (a cap below their grid). The top-level fields (`options`, `mode`, `calibration`, ...) are the latest run's.

A run's own output (`simulate --out`, `demo`) is that run alone.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

IDLE_HARNESS = "claude-code"  # `idle_compaction` is Claude Code's setting


def harness_info(d: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per harness with groups in `d` (a result or a lens summary): its mode, the newest turn analysed, the
    calibration that made it and, for a result, its options. Files written before `harnesses` existed get the whole
    run's values."""
    have = d.get("harnesses")
    have = have if isinstance(have, dict) else {}
    out: dict[str, dict[str, Any]] = {}
    for g in d.get("groups") or []:
        h = g["harness"]
        if isinstance(have.get(h), dict):
            out[h] = have[h]
            continue
        if h not in out:
            out[h] = {"mode": d.get("mode"), "data_through": None, "calibration": d.get("calibration")}
            if "options" in d:
                out[h]["options"] = d["options"]
        end = g["span"][1] if g.get("span") else d.get("data_through")
        if end is not None and (out[h]["data_through"] is None or end > out[h]["data_through"]):
            out[h]["data_through"] = end
    return out


def merge(prev: Any, new: dict[str, Any], covered: Iterable[str] | None) -> dict[str, Any]:
    """`new` plus the entries of `prev` for the harnesses `new` did not cover (`covered` None: it covered all). A
    previous file that is missing, of another schema, demo data against real or damaged in any way (any entry that
    cannot be read as written) is replaced: results are made again by the next run, and a background service must not
    stop on a bad file."""
    out = dict(new)
    out["harnesses"] = harness_info(new)
    if covered is None or not isinstance(prev, dict) or prev.get("schema") != new.get("schema"):
        return out
    if bool(prev.get("demo")) != bool(new.get("demo")):
        return out
    try:
        return _keep(prev, new, out, set(covered))
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return out


def _keep(prev: dict[str, Any], new: dict[str, Any], out: dict[str, Any], cov: set[str]) -> dict[str, Any]:
    out = dict(out)  # `out` stays as it was if this raises

    def kept(items: Any) -> list[Any]:  # raises on anything not written by this code: the file is then replaced
        if items is not None and not isinstance(items, list):
            raise TypeError(f"not a list: {type(items).__name__}")
        out = [x for x in items or [] if x["harness"] not in cov]
        if not all(isinstance(x["harness"], str) for x in out):
            raise TypeError("a harness that is not a name")
        return out

    old = kept(prev["groups"])
    if not old:
        return out
    for k in ("groups", "sessions"):
        if k in new:  # stable: each harness's entries keep their order; harnesses sorted, as analyze() emits them
            out[k] = sorted([*new[k], *kept(prev.get(k))], key=lambda x: x["harness"])
    info = harness_info(prev)
    out["harnesses"] = dict(sorted({**{h: info[h] for h in {g["harness"] for g in old}}, **out["harnesses"]}.items()))
    if "global_keys" in new:
        gk = {h: v for h, v in (prev.get("global_keys") or {}).items() if h not in cov}
        out["global_keys"] = dict(sorted({**gk, **new["global_keys"]}.items()))
    if "idle_compaction" in new and IDLE_HARNESS not in cov:
        out["idle_compaction"] = prev.get("idle_compaction")
    if isinstance(new.get("model_notes"), dict):  # per-harness lists merge; the rest (fallback prices, ...) is new
        notes, was = dict(new["model_notes"]), prev.get("model_notes") or {}
        for k, v in notes.items():
            w = was.get(k)
            if isinstance(v, list) and isinstance(w, list) and all("harness" in x for x in [*v, *w]):
                notes[k] = sorted([*v, *kept(w)], key=lambda x: x["harness"])
        out["model_notes"] = notes
    return out


def update(path: Path, new: dict[str, Any], covered: Iterable[str] | None) -> dict[str, Any]:
    """Merge `new` into the file at `path` and replace it atomically; returns what was written. A lock on a file
    beside it (`<name>.lock`) keeps two writers (a `simulate` and the service) from losing each other's harnesses."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # held for one read and one write of a small file
        try:
            prev = json.loads(path.read_text()) if path.exists() else None
        except (OSError, ValueError):
            prev = None  # unreadable: replaced
        out = merge(prev, new, covered)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(out, sort_keys=True))
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    return out
