"""Ready-to-paste configuration from a result: one Recommender per harness.

Recommenders only produce text. Writing files stays with `compactlab apply` (pre-authorized).
Every key a snippet uses must exist in data/config_knobs.json with a source and a verified-on date.
"""

from __future__ import annotations

import abc
import json
from dataclasses import asdict, dataclass, field
from datetime import date
from importlib import resources
from typing import Any, ClassVar

SCOPES = ("global", "project", "agent")
TIERS = ("headroom", "standard")
FORMATS = ("json", "toml", "yaml", "env", "shell")

_REG: dict[str, type[Recommender]] = {}
_KNOBS: dict[str, Any] | None = None


def knobs() -> dict[str, Any]:
    global _KNOBS
    if _KNOBS is None:
        _KNOBS = json.loads(resources.files("compactlab").joinpath("data/config_knobs.json").read_text())
    return _KNOBS


@dataclass
class Snippet:
    harness: str
    scope: str
    tier: str
    format: str
    title: str
    target: str
    content: str
    merge: str = ""
    revert: str = ""
    evidence: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    knobs: list[str] = field(default_factory=list)

    def text(self) -> str:
        """Plain-text rendering used by --print / --copy."""
        out = [f"# {self.title}", f"# target: {self.target}"]
        out += [f"# evidence: {e}" for e in self.evidence]
        out += [f"# WARNING: {w}" for w in self.warnings]
        out += [f"# note: {n}" for n in self.notes]
        out += ["", self.content.rstrip()]
        if self.merge:
            out += ["", "# merge without touching other keys:", self.merge]
        if self.revert:
            out += ["", "# revert:", self.revert]
        return "\n".join(out) + "\n"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"text": self.text()}


@dataclass
class Omitted:
    harness: str
    reason: str


class Recommender(abc.ABC):
    harness: ClassVar[str] = ""
    formats: ClassVar[dict[str, tuple[str, ...]]] = {}  # scope -> supported formats

    @abc.abstractmethod
    def generate(
        self, result: dict[str, Any], scope: str, tier: str, fmt: str | None, provider: str
    ) -> list[Snippet | Omitted]:
        """Snippets for this harness. fmt None = the scope's native format."""


def register(cls: type[Recommender]) -> type[Recommender]:
    _REG[cls.harness] = cls
    return cls


def recommenders() -> dict[str, type[Recommender]]:
    from . import claude_code, codex  # noqa: F401  (register)

    return dict(_REG)


# ---------- shared helpers ----------


def k(v: int | None) -> str:
    return "-" if v is None else f"{v // 1000}k"


def evidence(g: dict[str, Any], tier: str) -> str:
    cmp = g["comparison"][tier]
    vd = cmp["vs_default"]
    t = g["tiers"][tier]
    d = g["default"]
    tm = g.get("time_model", {})
    sign = "+" if vd["d_cost"] >= 0 else "-"
    return (
        f"{g['model']} · {g['role']}: {g['n_sessions']} sessions, {g['n_turns']} turns; default {k(d['threshold'])} -> "
        f"{tier} {k(t['threshold'])}: cost {sign}${abs(vd['d_cost']):,.0f} ({vd['d_cost_pct'] * 100:+.0f}%), "
        f"time {vd['d_time_s'] / 3600:+.1f}h, compactions {vd['compactions']} vs {vd['ref_compactions']}, "
        f"recall {t['recall']:.3f} vs {d['recall']:.3f} (assumed); time confidence {tm.get('turn_confidence', '?')}"
    )


def safe_value(g: dict[str, Any], tier: str, result: dict[str, Any], lo: int, hi: int) -> tuple[int | None, str]:
    """Value to emit, or (None, reason). Never outside [lo, hi], the window/effective max, or the configured cap."""
    cmp = g["comparison"][tier]
    if cmp["action"] != "set":
        return None, f"{g['model']} · {g['role']}: {cmp['action']}"
    v = int(g["tiers"][tier]["threshold"])
    cap = (result.get("options") or {}).get("headroom_cap")
    limit = min(x for x in (hi, g.get("effective_max"), g.get("max_window"), cap) if x)
    if v < lo or v > limit:
        return None, f"{g['model']} · {g['role']}: {v} outside allowed range [{lo}, {limit}]; not emitted"
    return v, ""


def generate(
    result: dict[str, Any],
    harness: str | None = None,
    scope: str = "global",
    tier: str = "headroom",
    fmt: str | None = None,
    provider: str = "default",
) -> list[Snippet | Omitted]:
    out: list[Snippet | Omitted] = []
    for name, cls in sorted(recommenders().items()):
        if harness and name != harness:
            continue
        if not any(g["harness"] == name for g in result["groups"]):
            continue
        out += cls().generate(result, scope, tier, fmt, provider)
    return out


def generate_all(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Every harness × scope × tier × native/alternative format, for the report's copy panel."""
    rows: list[dict[str, Any]] = []
    for name, cls in sorted(recommenders().items()):
        if not any(g["harness"] == name for g in result["groups"]):
            continue
        for scope in SCOPES:
            for tier in TIERS:
                for fmt in cls.formats.get(scope, ()):
                    for item in cls().generate(result, scope, tier, fmt, "default"):
                        if isinstance(item, Snippet):
                            rows.append(item.to_dict())
                        else:
                            rows.append({"harness": item.harness, "scope": scope, "tier": tier, "format": fmt,
                                         "omitted": item.reason})  # fmt: skip
    return rows


def check(snippets: list[Snippet], max_age_days: int = 90, today: date | None = None) -> list[str]:
    """Problems with the knobs a set of snippets relies on: unverified, missing, or verified too long ago."""
    today = today or date.today()
    data = knobs()["knobs"]
    problems = []
    for key in sorted({kk for s in snippets for kk in s.knobs}):
        kn = data.get(key)
        if kn is None:
            problems.append(f"{key}: not in config_knobs.json")
            continue
        if kn["status"] not in ("documented", "public-source"):
            problems.append(f"{key}: status {kn['status']} ({kn.get('note', '')})")
        age = (today - date.fromisoformat(kn["verified_on"])).days
        if age > max_age_days:
            problems.append(f"{key}: verified {age} days ago (> {max_age_days}); re-check {kn['source']}")
    return problems
