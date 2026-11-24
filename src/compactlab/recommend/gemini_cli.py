"""Gemini CLI recommender: `model.compressionThreshold` in settings.json, a fraction of the model's token limit.

The key is global, so one fraction serves every model; the most-used model sets it and the notes say where it lands
for the others. Experimental: the adapter has not been checked against a real Gemini CLI log, and `apply` does not
write Gemini settings: recommend-only.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from . import Omitted, Recommender, Snippet, evidence, knobs, register, safe_value
from .units import fraction_for, shared_fraction

KN = "gemini.settings.model.compressionThreshold"
TARGETS = {"global": "~/.gemini/settings.json", "project": ".gemini/settings.json in the project root"}
PATHS = {"global": "~/.gemini/settings.json", "project": ".gemini/settings.json"}
PLACES = 3


def _merge_py(path: str, v: float) -> str:
    code = (
        "import json,os,sys;p=os.path.expanduser(sys.argv[1]);"
        "d=json.load(open(p)) if os.path.exists(p) and open(p).read().strip() else {};"
        "d.setdefault('model',{})['compressionThreshold']=float(sys.argv[2]);"
        "open(p,'w').write(json.dumps(d,indent=2)+'\\n')"
    )
    return f'cp {path} {path}.bak 2>/dev/null; python3 -c "{code}" {path} {v:g}'


@register
class GeminiRecommender(Recommender):
    harness = "gemini-cli"
    formats: ClassVar[dict[str, tuple[str, ...]]] = {"global": ("json",), "project": ("json",)}

    def setting(self, g: dict[str, Any], threshold: int) -> str:
        f = fraction_for(threshold, int(g["max_window"]), PLACES)
        return json.dumps({"model": {"compressionThreshold": f.value}}, indent=2)

    def generate(self, result: dict[str, Any], scope: str, profile: str, fmt: str | None, provider: str) -> list:
        if scope not in self.formats:
            return [Omitted(self.harness, "Gemini CLI has one compression threshold for every model and agent; use "
                                          "the global or project snippet")]  # fmt: skip
        fmt = fmt or "json"
        if fmt != "json":
            return [Omitted(self.harness, f"format {fmt} is not available; settings.json is JSON")]
        out: list[Snippet | Omitted] = []
        picks: list[tuple[dict[str, Any], int]] = []
        for g in sorted(result["groups"], key=lambda g: -g["n_turns"]):
            if g["harness"] != self.harness or g["role"] != "main":
                continue
            v, why = safe_value(g, profile, result, 1, 10**9)
            if v is None:
                out.append(Omitted(self.harness, why))
            else:
                picks.append((g, v))
        if not picks:
            return out or [Omitted(self.harness, "no Gemini CLI main sessions in this history")]
        f, others = shared_fraction([(g["model"], v, int(g["max_window"])) for g, v in picks], PLACES)
        if not 0 < f.value <= 1:
            return [*out, Omitted(self.harness, f"{picks[0][0]['model']}: fraction {f.value:g} is outside (0, 1]")]
        g, v = picks[0]
        w = int(g["max_window"])
        notes = [
            f"{f.value:g} of {w:,} triggers at {f.tokens:,} prompt tokens (the pick is {v:,}); this value follows "
            f"{g['model']} (most turns).",
            *([f"Also seen: {x}" for x in others]),
            "Gemini CLI compares the last request's prompt tokens, cached ones included, as the replay does.",
            "Restart Gemini CLI after the change. A system settings file (/Library/Application Support/GeminiCli/"
            "settings.json on macOS) overrides the user and project files.",
        ]
        if f.note:
            notes.append(f.note)
        warn = [
            "Experimental: built from Gemini CLI's source; no real Gemini CLI log has been replayed yet.",
            str(knobs()["knobs"][KN]["note"]),
        ]
        path = PATHS[scope]
        content = json.dumps({"model": {"compressionThreshold": f.value}}, indent=2)
        out.insert(0, Snippet(self.harness, scope, profile, fmt, f"Gemini CLI · {scope} · {profile}: compression",
                              TARGETS[scope], content, _merge_py(path, f.value),
                              f"delete model.compressionThreshold from {path} (or restore {path}.bak)",
                              [evidence(g, profile) for g, _ in picks], notes, warn, [KN]))  # fmt: skip
        return out
