"""Claude Code recommender: settings.json (global / project), subagent frontmatter, env var, CLI flag."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from . import Omitted, Recommender, Snippet, evidence, k, knobs, register, safe_value

LO, HI = 100_000, 1_000_000
KN_MODEL = "claude.settings.modelSettings.autoCompactWindow"
KN_AGENT = "claude.agent.autoCompactWindow"
KN_ENV = "claude.env.CLAUDE_CODE_AUTO_COMPACT_WINDOW"
KN_FLAG = "claude.flag.--autocompact"
TARGETS = {
    "global": "~/.claude/settings.json",
    "project": ".claude/settings.local.json (personal) or .claude/settings.json (shared, commit it)",
}


def _merge_py(path: str, values: dict[str, int]) -> str:
    code = (
        "import json,os,sys;p=os.path.expanduser(sys.argv[1]);"
        "d=json.load(open(p)) if os.path.exists(p) and open(p).read().strip() else {};"
        "[d.setdefault('modelSettings',{}).setdefault(m,{}).update(autoCompactWindow=v) "
        "for m,v in json.loads(sys.argv[2]).items()];"
        "open(p,'w').write(json.dumps(d,indent=2)+'\\n')"
    )
    return f"cp {path} {path}.bak 2>/dev/null; python3 -c \"{code}\" {path} '{json.dumps(values)}'"


def _revert_py(path: str, models: list[str]) -> str:
    code = (
        "import json,os,sys;p=os.path.expanduser(sys.argv[1]);d=json.load(open(p));"
        "[d.get('modelSettings',{}).get(m,{}).pop('autoCompactWindow',None) for m in sys.argv[2:]];"
        "open(p,'w').write(json.dumps(d,indent=2)+'\\n')"
    )
    return (
        f'python3 -c "{code}" {path} {" ".join(models)}   # or run /autocompact auto in Claude Code for the user scope'
    )


@register
class ClaudeCodeRecommender(Recommender):
    harness = "claude-code"
    formats: ClassVar[dict[str, tuple[str, ...]]] = {
        "global": ("json", "env", "shell"),
        "project": ("json",),
        "agent": ("yaml",),
    }

    def generate(self, result: dict[str, Any], scope: str, profile: str, fmt: str | None, provider: str) -> list:
        fmt = fmt or self.formats[scope][0]
        if fmt not in self.formats.get(scope, ()):
            return [Omitted(self.harness, f"format {fmt} is not available for scope {scope}; "
                                          f"use one of {', '.join(self.formats[scope])}")]  # fmt: skip
        role = "subagent" if scope == "agent" else "main"
        groups = [g for g in result["groups"] if g["harness"] == self.harness and g["role"] == role]
        values: dict[str, int] = {}
        out: list[Snippet | Omitted] = []
        ev = []
        for g in groups:
            v, why = safe_value(g, profile, result, LO, HI)
            if v is None:
                out.append(Omitted(self.harness, why))
                continue
            values[g["model"]] = v
            ev.append(evidence(g, profile))
        if not values:
            return out or [Omitted(self.harness, f"no {role} groups for Claude Code")]
        prov = knobs()["providers"]["claude-code"]
        pnote = prov.get(provider, prov["anthropic"]) if provider != "default" else prov["anthropic"]
        notes = [pnote, "Gateway or custom model IDs: " + prov["gateway"]]
        if scope in ("global", "project") and fmt == "json":
            frag = {"modelSettings": {m: {"autoCompactWindow": v} for m, v in values.items()}}
            path = "~/.claude/settings.json" if scope == "global" else ".claude/settings.local.json"
            notes.insert(0, "Needs Claude Code v2.1.288+. Merge into the existing file; do not replace it.")
            if scope == "project":
                notes.insert(1, "Use .claude/settings.json instead to share with your team (commit it).")
            out.insert(0, Snippet(
                self.harness, scope, profile, fmt, f"Claude Code · {scope} · {profile}: per-model auto-compact window",
                TARGETS[scope], json.dumps(frag, indent=2), _merge_py(path, values), _revert_py(path, list(values)),
                ev, notes, [], [KN_MODEL]))  # fmt: skip
        elif fmt == "env":
            lines = [f"# {m}: export only when this model is the one you run (applies to every model in the session)\n"
                     f"export CLAUDE_CODE_AUTO_COMPACT_WINDOW={v}" for m, v in values.items()]  # fmt: skip
            notes.insert(0, "Precedence: the env var beats --autocompact, /autocompact and settings files.")
            out.insert(0, Snippet(self.harness, scope, profile, fmt, f"Claude Code · env var · {profile}",
                                  "shell profile or CI environment", "\n".join(lines), "",
                                  "unset CLAUDE_CODE_AUTO_COMPACT_WINDOW", ev, notes, [], [KN_ENV]))  # fmt: skip
        elif fmt == "shell":
            lines = [f"claude --model {m} --autocompact {v // 1000}k" for m, v in values.items()]
            notes.insert(0, "One launch only; saved settings are unchanged. The env var still wins if set.")
            out.insert(0, Snippet(self.harness, scope, profile, fmt, f"Claude Code · CLI flag · {profile}",
                                  "command line",
                                  "\n".join(lines), "", "launch without --autocompact (or --autocompact auto)",
                                  ev, notes, [], [KN_FLAG]))  # fmt: skip
        elif scope == "agent":
            blocks = [f"# subagents whose model is {m}:\nautoCompactWindow: {v}" for m, v in values.items()]
            notes.insert(0, "Add the line to the YAML frontmatter of each subagent file using that model "
                            "(~/.claude/agents/*.md or .claude/agents/*.md). "
                            "Needs v2.1.296+. It can only lower the window.")  # fmt: skip
            out.insert(0, Snippet(self.harness, scope, profile, fmt, f"Claude Code · subagents · {profile}",
                                  "~/.claude/agents/<name>.md or .claude/agents/<name>.md (frontmatter)",
                                  "\n\n".join(blocks), "", "delete the autoCompactWindow line from the frontmatter",
                                  ev, notes, [], [KN_AGENT]))  # fmt: skip
        return out


__all__ = ["ClaudeCodeRecommender", "k"]
