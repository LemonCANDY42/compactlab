"""Self-contained HTML report: template.html (inline CSS/JS/SVG, no network) + the result JSON."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from importlib import resources

TEMPLATE = resources.files("compactlab.report").joinpath("template.html").read_text()


def render_html(result: dict, redact: bool = False) -> str:
    """Deterministic: the same result renders to the same bytes (no wall-clock timestamps)."""
    data = json.loads(json.dumps(result))
    ts = data.get("data_through")
    data["generated"] = ("data through " + datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")) if ts else ""
    from ..recommend import generate_all, setting

    data["recommendations"] = generate_all(data)
    for g in data["groups"]:  # the Copy button's text for every slider position, from the harness's recommender
        g["settings"] = [setting(g, T) for T in g["grid"]]
    if redact:
        for s in data.get("sessions", []):
            s["id"] = "s-" + hashlib.sha256(s["id"].encode()).hexdigest()[:10]
    payload = json.dumps(data, separators=(",", ":"), sort_keys=True).replace("</", "<\\/")
    return TEMPLATE.replace("__DATA__", payload)
