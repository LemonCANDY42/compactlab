"""The engine's settings commands: `plan`, `apply`, `revert`, `history`, `verify` and `consent` (docs/ENGINE_API.md).

Writing goes through compactlab.apply as everywhere else (backup, atomic write, manifest, revert); these functions
only choose the targets and shape the messages. The person clicking Apply on a reviewed plan is the authorization:
the targets are the harnesses' own config files, the models the result has groups for, and the usual bounds.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .. import apply as ap
from . import message, result_message

APPLY_BOUNDS = (100_000, 1_000_000)  # the CLI's --i-authorize defaults
WRITABLE = ("claude-code", "codex")


def preauth(configs: dict[str, Path], res: dict[str, Any]) -> ap.Preauth:
    """Every main-session model of the result, in the harness's config file, within the usual bounds."""
    files = {h: p for h, p in configs.items() if h in WRITABLE}
    models = sorted({ap.model_key(g["harness"], g["model"]) for g in res["groups"] if g["role"] == "main"})
    pre = ap.explicit_preauth(files, models or ["-"], *APPLY_BOUNDS)
    pre.source = "app"
    return pre


def _impact(summary: dict[str, Any], ch: ap.Change) -> dict[str, Any]:
    """What the change does per day, against the setting in force (negative: cheaper / faster), and its evidence."""
    gk = (summary.get("global_keys") or {}).get(ch.harness)
    mains = [g for g in summary["groups"] if g["harness"] == ch.harness and g["role"] == "main"]
    if gk:  # one key for every model: the decision across them, against the default
        p = gk["profiles"].get(summary["profile"]) or {}
        return {
            "against": "default",
            "usd_per_day": p.get("d_usd_per_day"),
            "min_per_day": p.get("d_min_per_day"),
            "cost_pct": None,
            "time_pct": None,
            "days": max((g.get("active_days") or g["days"] for g in mains), default=None),
            "sessions": sum(g.get("n_sessions") or 0 for g in mains) or None,
        }
    g = next((g for g in mains if ap.model_key(g["harness"], g["model"]) == ch.model), None)
    if g is None:
        return {"against": None, "usd_per_day": None, "min_per_day": None, "cost_pct": None, "time_pct": None,
                "days": None, "sessions": None}  # fmt: skip
    pick = g["profiles"][g["selected"]]
    vs, against = (pick["vs_current"], "current") if pick.get("vs_current") else (pick.get("vs_default"), "default")
    vs = vs or {}
    return {
        "against": against,
        "usd_per_day": vs.get("usd_per_day"),
        "min_per_day": vs.get("min_per_day"),
        "cost_pct": vs.get("cost_pct"),
        "time_pct": vs.get("time_pct"),
        "days": g.get("active_days") or g["days"],
        "sessions": g.get("n_sessions"),
    }


def plan_message(res: dict[str, Any], changes: list[ap.Change], rejected: list[str], profile: str) -> dict[str, Any]:
    summary = result_message(res)
    rendered = ap.render(changes) if changes else {}
    return message(
        "plan",
        profile=profile,
        plan_hash=ap.plan_hash(changes),
        changes=[
            {
                "harness": c.harness,
                "model": c.model,
                "file": str(c.file),
                "key": c.key,
                "old": c.old,
                "new": c.new,
                "impact": _impact(summary, c),
                "plan_hash": ap.plan_hash([c]),  # apply just this one: --only harness:model --expect-plan this
            }
            for c in changes
        ],
        skipped=rejected,
        diff=ap.diff_text(rendered) if rendered else "",
        notes=ap.formatting_note(rendered, changes) if rendered else [],
    )


def applied_message(manifest: Path, changes: list[ap.Change]) -> dict[str, Any]:
    return message(
        "applied",
        id=manifest.stem,
        changes=[{"harness": c.harness, "model": c.model, "key": c.key, "old": c.old, "new": c.new} for c in changes],
    )


def record_applied(state_dir: Path, manifest: Path, changes: list[ap.Change], profile: str) -> None:
    """Remember what compactlab wrote, as auto-apply does: its manual-edit check and cooldown then see this change."""
    from ..service.policy import journal, load_applied

    p = state_dir / "service" / "applied.json"
    applied = load_applied(p)
    now = time.time()
    for c in changes:
        applied[f"{c.file}::{c.model}"] = {"value": c.new, "at": now, "manifest": manifest.stem}
        journal(state_dir / "service" / "journal.jsonl",
                {"model": c.model, "file": str(c.file), "key": c.key, "old": c.old, "new": c.new,
                 "manifest": manifest.stem, "profile": profile, "by": "app"})  # fmt: skip
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(applied, indent=2))


def _status(now: object, old: object, new: object) -> str:
    if now == new:
        return "in_effect"
    return "reverted" if now == old else "changed"


def history_message(state_dir: Path, limit: int) -> dict[str, Any]:
    """The latest applies, newest first, each key with the value its file holds now: `in_effect`, `reverted` or
    `changed` (edited since by someone else: drift)."""
    out = []
    for m in sorted((state_dir / "manifests").glob("*.json"), reverse=True)[:limit]:
        try:
            d = json.loads(m.read_text())
        except (OSError, ValueError):
            continue
        rows = []
        for e in d.get("entries", []):
            f = Path(e["file"])
            for ch in e.get("changes", []):
                try:
                    now = ap._read_current(f, ch["harness"], ch["model"])
                except (ap.ApplyError, OSError, ValueError):
                    now = "unreadable"
                rows.append({
                    "harness": ch["harness"], "model": ch["model"], "file": str(f), "key": ch["key"],
                    "old": ch["old"], "new": ch["new"], "now": now, "status": _status(now, ch["old"], ch["new"]),
                })  # fmt: skip
        out.append({"id": d.get("id", m.stem), "at": d.get("created"), "by": d.get("preauth"), "changes": rows})
    return message("history", applies=out)


def reverted_message(manifest: Path, log: list[str]) -> dict[str, Any]:
    return message("reverted", id=manifest.stem, log=log, complete=not any("skipped" in x for x in log))


def verification_message(manifest_id: str, res: dict[str, Any]) -> dict[str, Any]:
    """verify's verdict per model, without the per-session detail."""
    keep = ("harness", "model", "role", "old", "new", "verdict", "summary", "in_effect")
    groups = []
    for g in res["groups"]:
        row = {k: g.get(k) for k in keep}
        obs, pred = g.get("observed") or {}, g.get("predicted") or {}
        row["cost_pct"] = obs.get("cost_pct")
        row["cost_pct_predicted"] = pred.get("cost_pct")
        row["sessions_before"] = (g.get("before") or {}).get("sessions")
        row["sessions_after"] = (g.get("after") or {}).get("sessions")
        groups.append(row)
    return message("verification", id=manifest_id, applied_at=res["applied_at"], groups=groups)


def consent_message(consent: Any) -> dict[str, Any]:
    from dataclasses import asdict

    return message("consent", consent=asdict(consent) if consent is not None else None)
