"""Pre-authorized auto-apply: stored consent, anti-flapping rules, manual-edit detection, change journal.

Nothing here writes a settings file directly: an accepted decision goes through compactlab.apply (backup, atomic
write, manifest that `compactlab revert` understands).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .. import apply as ap
from ..sim.select import PROFILE_NAMES, PROFILES

CONSENT_VERSION = 1
KEYS = {"claude-code": ["modelSettings.*.autoCompactWindow"], "codex": [ap.CODEX_KEY]}
# flags that mean the pick cannot be trusted for an unattended change
UNSAFE_FLAGS = {"recall_floor_not_met", "wait_cap_unreachable", "min_window_unreachable", "wait_over_cap"}


@dataclass
class Consent:
    """What the person allowed: one harness settings file, these models, this profile."""

    harness: str
    file: str
    models: list[str]
    profile: str = "balanced"
    created: str = field(default_factory=lambda: date.today().isoformat())
    expires: str | None = None
    version: int = CONSENT_VERSION

    def preauth(self) -> ap.Preauth:
        lo, hi = ap.CLAUDE_LIMITS
        t = ap.Target(self.harness, ap._p(self.file), list(KEYS[self.harness]), list(self.models), lo, hi)
        return ap.Preauth([t], allow_retune=True, source="consent")


def save_consent(path: Path, c: Consent) -> None:
    if c.harness not in KEYS:
        raise ap.ApplyError(f"consent: unknown harness {c.harness!r}")
    if c.profile not in PROFILE_NAMES:
        raise ap.ApplyError(f"consent: unknown profile {c.profile!r}")
    if not c.models:
        raise ap.ApplyError("consent: name at least one model")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(c), indent=2))
    path.chmod(0o600)


def load_consent(path: Path) -> Consent:
    if not path.exists():
        raise ap.ApplyError(f"no consent file at {path}; create one with: compactlab consent set ...")
    d = json.loads(path.read_text())
    if d.get("version") != CONSENT_VERSION:
        raise ap.ApplyError("consent file: unsupported version")
    c = Consent(
        **{k: d[k] for k in ("harness", "file", "models", "profile", "created", "expires", "version") if k in d}
    )
    if c.expires and date.fromisoformat(c.expires) < date.today():
        raise ap.ApplyError("consent file has expired")
    if c.harness not in KEYS or c.profile not in PROFILE_NAMES:
        raise ap.ApplyError("consent file: unknown harness or profile")
    return c


@dataclass
class Rules:
    """Anti-flapping settings. Defaults are deliberately slow: a setting moves at most weekly, in small steps."""

    min_days: float = 7.0
    min_turns: int = 500
    dead_band: int = 25_000  # moves within one grid step plus this are ignored
    min_benefit_pct: float = 2.0  # of the current list-price cost per day...
    min_benefit_min: float = 2.0  # ...or this many minutes per day
    max_step: int = 100_000
    cooldown_s: float = 7 * 86400.0


@dataclass
class Decision:
    model: str
    action: str  # "apply" | "skip" | "notify"
    reason: str
    current: int | None = None
    target: int | None = None
    value: int | None = None


def _idx(grid: list[int], v: int) -> int:
    return min(range(len(grid)), key=lambda i: abs(grid[i] - v))


def decide(
    g: dict[str, Any],
    profile: str,
    current: int | None,
    last_written: int | None,
    last_applied_at: float | None,
    now: float,
    rules: Rules,
    force: bool = False,
) -> Decision:
    """Whether to move one model's setting, to what, and why not. `current` is what the settings file holds now
    (None: not set, so the harness default applies); `last_written` is what compactlab last wrote there."""
    m = g["model"]
    p = g["profiles"][profile]
    target = int(p["threshold"])
    d = Decision(m, "skip", "", current, target)
    if g["days"] < rules.min_days or g["n_turns"] < rules.min_turns:
        need = f"{rules.min_days:g} days / {rules.min_turns} turns"
        d.reason = f"too little history ({g['days']:.1f} days, {g['n_turns']} turns; need {need})"
        return d
    tm = g.get("time_model") or {}
    if tm.get("compact_source") == "prior" or tm.get("turn_source") == "prior":
        d.reason = "time model is a prior (no usable timing data): low confidence"
        return d
    bad = UNSAFE_FLAGS.intersection(p["flags"])
    if bad:
        d.reason = "profile flags " + ", ".join(sorted(bad))
        return d
    if g["comparison"][profile]["action"] in ("no action needed", "keep default") and current is None:
        d.reason = g["comparison"][profile]["action"]
        return d
    if last_written is not None and current != last_written and not force:
        d.action = "notify"
        d.reason = f"edited by someone else (now {current!r}, compactlab last wrote {last_written}); not overwriting"
        return d
    if last_applied_at is not None and now - last_applied_at < rules.cooldown_s and not force:
        d.reason = f"cooldown: last applied {(now - last_applied_at) / 86400:.1f} days ago"
        return d
    grid = g["grid"]
    step = grid[1] - grid[0] if len(grid) > 1 else 25_000
    cur_eff = current if current is not None else int(g["default"]["threshold"])
    if abs(target - cur_eff) < step + rules.dead_band:
        d.reason = f"within the dead-band ({cur_eff} -> {target})"
        return d
    # move at most max_step toward the target, on the grid
    move = max(-rules.max_step, min(rules.max_step, target - cur_eff))
    value = grid[_idx(grid, cur_eff + move)]
    i, j = grid.index(value), _idx(grid, cur_eff)
    days = max(float(g["days"]), 1.0)
    d_usd = (g["combined_cost"][j] - g["combined_cost"][i]) / days
    d_min = (g["time_s"][j] - g["time_s"][i]) / 60 / days
    cost_day = g["combined_cost"][j] / days
    if d_usd < cost_day * rules.min_benefit_pct / 100 and d_min < rules.min_benefit_min:
        d.reason = f"benefit too small ({d_usd:+.2f} $/day, {d_min:+.1f} min/day)"
        return d
    # each step stays inside the profile's guardrails, or (when the current setting is already outside them, as a
    # default above the recall floor's reach can be) at least moves strictly toward them
    spec = PROFILES[profile]
    floor = g["recommendation"]["recall_floor"]
    dflt = g["default"]

    def wait(k: int) -> float:
        extra = g["time_compact_s"][k] + g["time_redisc_s"][k] - dflt["time_compact_s"] - dflt["time_redisc_s"]
        return float(extra) / 60 / days

    recall_ok = g["recall"][i] >= floor or g["recall"][i] > g["recall"][j]
    wait_ok = not spec["enforce_wait_cap"] or wait(i) <= p["wait_cap_min_per_day"] or wait(i) < wait(j)
    floor_ok = value >= p["min_threshold"] or value > cur_eff
    if not (recall_ok and wait_ok and floor_ok):
        d.reason = f"{value} would break the {profile} guardrails"
        return d
    d.action, d.value = "apply", value
    d.reason = f"{cur_eff} -> {value} (target {target}): saves ${d_usd:.2f}/day, {d_min:.1f} min/day (- = adds)"
    return d


# ---------- state: what compactlab last wrote, and the journal ----------


def load_applied(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.exists() else {}


def journal(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"at": time.time(), **entry}) + "\n")


def run_auto_apply(
    result: dict[str, Any],
    consent: Consent,
    state_dir: Path,
    rules: Rules,
    now: float,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Decide per consented model; write the accepted changes through compactlab.apply (backup + manifest)."""
    applied_path = state_dir / "service" / "applied.json"
    applied = load_applied(applied_path)
    pre = consent.preauth()
    file = pre.targets[0].file
    decisions: list[Decision] = []
    for g in result["groups"]:
        if g["role"] != "main" or g["harness"] != consent.harness or g["model"] not in consent.models:
            continue
        cur = ap._read_current(file, consent.harness, g["model"])
        cur = cur if isinstance(cur, int) else None
        rec = applied.get(f"{file}::{g['model']}", {})
        decisions.append(decide(g, consent.profile, cur, rec.get("value"), rec.get("at"), now, rules, force))
    todo = [{"harness": consent.harness, "model": d.model, "value": d.value} for d in decisions if d.action == "apply"]
    out: dict[str, Any] = {
        "decisions": [asdict(d) for d in decisions],
        "dry_run": dry_run,
        "diff": "",
        "manifest": None,
    }
    if not todo:
        return out
    changes, rejected = ap.plan_changes(todo, pre)
    out["rejected"] = rejected
    if not changes:
        return out
    out["diff"] = ap.diff_text(ap.render(changes))
    if dry_run:
        return out
    manifest = ap.write_changes(changes, state_dir, pre)
    out["manifest"] = str(manifest)
    for c in changes:
        applied[f"{c.file}::{c.model}"] = {"value": c.new, "at": now, "manifest": manifest.stem}
        journal(state_dir / "service" / "journal.jsonl",
                {"model": c.model, "file": str(c.file), "key": c.key, "old": c.old, "new": c.new,
                 "manifest": manifest.stem, "profile": consent.profile})  # fmt: skip
    applied_path.parent.mkdir(parents=True, exist_ok=True)
    applied_path.write_text(json.dumps(applied, indent=2))
    return out
