"""OpenCode recommender: a per-model `limit` in opencode.json (OpenCode 1.x), with the 2.x `compaction.buffer` share.

OpenCode 1.x has no threshold key of its own. It compacts at `limit.input - compaction.reserved` for a model whose
limit sets `input`, else at `context - max output`, and `compaction.reserved` (global) does nothing for a model
without `limit.input`. A per-model `limit` with `input = threshold + reserved` sets the point exactly. `limit` needs
`context` and `output` too: context is the window compactlab used, output is OpenCode's own cap (32,000; a larger
value changes nothing). OpenCode 2.x (pre-release) replaces this with a global `compaction.buffer`, a share of the
window kept free, which the notes give. `apply` does not write OpenCode config: recommend-only.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from ..sim.baseline import OPENCODE_OUTPUT_CAP, defaults
from . import Omitted, Recommender, Snippet, evidence, register, safe_value
from .units import free_share

KN_LIMIT = "opencode.config.provider.models.limit"
KN_BUFFER = "opencode.config.compaction.buffer"
TARGETS = {"global": "~/.config/opencode/opencode.json", "project": "opencode.json in the project root"}
PATHS = {"global": "~/.config/opencode/opencode.json", "project": "opencode.json"}


def _split(model: str) -> tuple[str, str]:
    prov, sep, mid = model.partition("/")
    return (prov, mid) if sep else ("<providerID>", model)


def limit_for(threshold: int, window: int) -> dict[str, int]:
    reserved = int(defaults()["opencode"]["reserved_default"])  # = min(20000, output) with output 32,000
    return {"context": window, "input": threshold + reserved, "output": OPENCODE_OUTPUT_CAP}


def config_for(picks: list[tuple[str, int, int]]) -> dict[str, Any]:
    prov: dict[str, Any] = {}
    for model, threshold, window in picks:
        p, m = _split(model)
        prov.setdefault(p, {"models": {}})["models"][m] = {"limit": limit_for(threshold, window)}
    return {"provider": prov}


def _merge_py(path: str, cfg: dict[str, Any]) -> str:
    code = (
        "import json,os,sys;p=os.path.expanduser(sys.argv[1]);"
        "d=json.load(open(p)) if os.path.exists(p) and open(p).read().strip() else {};"
        "[d.setdefault('provider',{}).setdefault(pv,{}).setdefault('models',{}).setdefault(m,{})"
        ".update(limit=x['limit']) for pv,pm in json.loads(sys.argv[2])['provider'].items() "
        "for m,x in pm['models'].items()];"
        "open(p,'w').write(json.dumps(d,indent=2)+'\\n')"
    )
    return f"cp {path} {path}.bak 2>/dev/null; python3 -c \"{code}\" {path} '{json.dumps(cfg)}'"


@register
class OpenCodeRecommender(Recommender):
    harness = "opencode"
    formats: ClassVar[dict[str, tuple[str, ...]]] = {"global": ("json",), "project": ("json",)}

    def setting(self, g: dict[str, Any], threshold: int) -> str:
        return json.dumps(config_for([(g["model"], threshold, int(g["max_window"]))]), indent=2)

    def generate(self, result: dict[str, Any], scope: str, profile: str, fmt: str | None, provider: str) -> list:
        if scope not in self.formats:
            return [Omitted(self.harness, "OpenCode has no per-agent compaction setting; the per-model limit in the "
                                          "global or project config applies to subagents too")]  # fmt: skip
        fmt = fmt or "json"
        if fmt != "json":
            return [Omitted(self.harness, f"format {fmt} is not available; opencode.json is JSON")]
        out: list[Snippet | Omitted] = []
        picks: list[tuple[str, int, int]] = []
        ev, notes = [], []
        for g in sorted(result["groups"], key=lambda g: -g["n_turns"]):
            if g["harness"] != self.harness or g["role"] != "main":
                continue
            v, why = safe_value(g, profile, result, 1, 10**9)
            if v is None:
                out.append(Omitted(self.harness, why))
                continue
            w = int(g["max_window"])
            picks.append((g["model"], v, w))
            ev.append(evidence(g, profile))
            buf = free_share(v, w)
            src = g.get("window_source", "?")
            notes.append(f"{g['model']}: context {w:,} is the window compactlab used ({src}); correct it if "
                         f"OpenCode's model catalog says otherwise. On OpenCode 2.x set compaction.buffer to "
                         f"\"{buf.value:g}%\" instead (keeps {w - buf.tokens:,} free, compacts at {buf.tokens:,}; "
                         "global for every model)" + (f"; {buf.note}" if buf.note else ""))  # fmt: skip
        if not picks:
            return out or [Omitted(self.harness, "no OpenCode main sessions in this history")]
        cfg = config_for(picks)
        reserved = int(defaults()["opencode"]["reserved_default"])
        notes += [
            f"OpenCode 1.x compacts at limit.input - compaction.reserved; with reserved unset it is {reserved:,}, so "
            f"input is the threshold + {reserved:,}. If you set compaction.reserved, add that instead.",
            "OpenCode counts the last reply's output as well as its context, so it compacts up to one reply earlier "
            "than the replay.",
            "Subagent sessions on the same model use the same limit.",
        ]
        warn = [
            f"limit replaces the model's catalog limits: output is set to {OPENCODE_OUTPUT_CAP:,} (OpenCode's own "
            "cap); use the model's real max output if it is lower.",
            "A custom-provider model with no limit in your config has no known window, and OpenCode 1.x never "
            "compacts it by itself; this limit turns automatic compaction on for it.",
        ]
        path = PATHS[scope]
        models = [f"{_split(m)[0]}/{_split(m)[1]}" for m, _, _ in picks]
        out.insert(0, Snippet(self.harness, scope, profile, fmt, f"OpenCode · {scope} · {profile}: per-model limit",
                              TARGETS[scope], json.dumps(cfg, indent=2), _merge_py(path, cfg),
                              f"delete limit from provider.<id>.models.<model> for {', '.join(models)} in {path} "
                              f"(or restore {path}.bak)", ev, notes, warn, [KN_LIMIT, KN_BUFFER]))  # fmt: skip
        return out
