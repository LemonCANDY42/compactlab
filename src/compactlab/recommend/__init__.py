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

from ..sim.select import DEFAULT_PROFILE, PROFILE_NAMES

SCOPES = ("global", "project", "agent")
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
    profile: str
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
    def setting(self, g: dict[str, Any], threshold: int) -> str:
        """The bare setting for one group at one threshold, in this harness's own syntax (a quick copy)."""

    @abc.abstractmethod
    def generate(
        self, result: dict[str, Any], scope: str, profile: str, fmt: str | None, provider: str
    ) -> list[Snippet | Omitted]:
        """Snippets for this harness. fmt None = the scope's native format."""


def register(cls: type[Recommender]) -> type[Recommender]:
    _REG[cls.harness] = cls
    return cls


def recommenders() -> dict[str, type[Recommender]]:
    from . import claude_code, codex, dsh  # noqa: F401  (register)

    return dict(_REG)


# ---------- shared helpers ----------


def k(v: int | None) -> str:
    return "-" if v is None else f"{v // 1000}k"


def evidence(g: dict[str, Any], profile: str) -> str:
    cmp = g["comparison"][profile]
    vd = cmp["vs_default"]
    t = g["profiles"][profile]
    d = g["default"]
    tm = g.get("time_model", {})
    sign = "+" if vd["d_cost"] >= 0 else "-"
    return (
        f"{g['model']} · {g['role']}: {g['n_sessions']} sessions, {g['n_turns']} turns, {g['days']:.0f} days; "
        f"default {k(d['threshold'])} -> {profile} (${t['value_of_time']:g}/h) {k(t['threshold'])}: "
        f"list-price cost {sign}${abs(vd['d_cost']):,.0f} ({vd['d_cost_pct'] * 100:+.0f}%), "
        f"time {vd['d_time_s'] / 3600:+.1f}h ({vd['d_time_min_per_day']:+.0f} min/day), "
        f"compactions/day {vd['compactions_per_day']:.1f} vs {vd['ref_compactions_per_day']:.1f}, "
        f"recall {t['recall']:.3f} vs {d['recall']:.3f} (lite proxy); "
        f"time model {tm.get('turn_source', '?')}/{tm.get('compact_source', '?')}"
    )


def resolve_profile(result: dict[str, Any], profile: str | None) -> str:
    return profile or (result.get("options") or {}).get("profile") or DEFAULT_PROFILE


def safe_value(g: dict[str, Any], profile: str, result: dict[str, Any], lo: int, hi: int) -> tuple[int | None, str]:
    """Value to emit, or (None, reason). Never outside [lo, hi], the window/effective max, or the configured cap."""
    cmp = g["comparison"][profile]
    if cmp["action"] != "set":
        return None, f"{g['model']} · {g['role']}: {cmp['action']}"
    v = int(g["profiles"][profile]["threshold"])
    cap = (result.get("options") or {}).get("max_threshold")
    limit = min(x for x in (hi, g.get("effective_max"), g.get("max_window"), cap) if x)
    if v < lo or v > limit:
        return None, f"{g['model']} · {g['role']}: {v} outside allowed range [{lo}, {limit}]; not emitted"
    return v, ""


def setting(g: dict[str, Any], threshold: int) -> str:
    """The setting for one group at any threshold, from its harness's recommender (terminal `c`, report Copy)."""
    cls = recommenders().get(g["harness"])
    if cls is None:
        return f"# no recommender for harness {g['harness']}; set the threshold to {threshold} by hand"
    return cls().setting(g, threshold)


def generate(
    result: dict[str, Any],
    harness: str | None = None,
    scope: str = "global",
    profile: str | None = None,
    fmt: str | None = None,
    provider: str = "default",
) -> list[Snippet | Omitted]:
    """Snippets for one profile (default: the result's selected profile)."""
    profile = resolve_profile(result, profile)
    out: list[Snippet | Omitted] = []
    for name, cls in sorted(recommenders().items()):
        if harness and name != harness:
            continue
        if not any(g["harness"] == name for g in result["groups"]):
            continue
        out += cls().generate(result, scope, profile, fmt, provider)
    return out


def generate_all(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Every harness × scope × profile × native/alternative format, for the report's copy panel."""
    rows: list[dict[str, Any]] = []
    for name, cls in sorted(recommenders().items()):
        if not any(g["harness"] == name for g in result["groups"]):
            continue
        for scope in SCOPES:
            for profile in PROFILE_NAMES:
                for fmt in cls.formats.get(scope, ()):
                    for item in cls().generate(result, scope, profile, fmt, "default"):
                        if isinstance(item, Snippet):
                            rows.append(item.to_dict())
                        else:
                            rows.append({"harness": item.harness, "scope": scope, "profile": profile, "format": fmt,
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
