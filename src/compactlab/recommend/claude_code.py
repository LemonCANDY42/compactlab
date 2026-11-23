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
KN_IDLE = "claude.settings.idleCompaction"
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


def _idle_py(path: str, value: bool | None) -> str:
    """Set (or, with None, remove) the top-level idleCompaction key, leaving the rest of the file alone."""
    code = (
        "import json,os,sys;p=os.path.expanduser(sys.argv[1]);"
        "d=json.load(open(p)) if os.path.exists(p) and open(p).read().strip() else {};v=json.loads(sys.argv[2]);"
        "d.pop('idleCompaction',None) if v is None else d.update(idleCompaction=v);"
        "open(p,'w').write(json.dumps(d,indent=2)+'\\n')"
    )
    return f'python3 -c "{code}" {path} {json.dumps(value)}'


def idle_note(result: dict[str, Any], profile: str) -> tuple[dict[str, Any] | None, str]:
    """The idle-compaction verdict for a profile and one line about it ('' when it applies to no group)."""
    adv = result.get("idle_compaction")
    if not adv:
        return None, ""
    v = adv["profiles"][profile]
    on = adv["enabled"]
    flip = "turning it off" if on else "turning it on"
    act = ("turn it off" if on else "turn it on") if v["switch"] else ("keep it on" if on else "keep it off")
    sign = "+" if v["d_usd_per_day"] >= 0 else "-"
    return v, (
        f"Idle compaction (idleCompaction, now {'on' if on else 'off'}): {act}. With these picks, {flip} changes "
        f"list-price cost by {sign}${abs(v['d_usd_per_day']):,.1f}/day, wall-clock by "
        f"{v['d_min_per_day']:+.1f} min/day and compactions by {v['d_compactions_per_day']:+.1f}/day "
        f"({', '.join(adv['groups'])})."
    )


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

    def setting(self, g: dict[str, Any], threshold: int) -> str:
        return json.dumps({"modelSettings": {g["model"]: {"autoCompactWindow": threshold}}})

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
        idle_snip = self._idle_snippet(result, scope, profile, fmt)
        if not values:  # no window to change; the idle-compaction setting may still be worth flipping
            return [
                *([idle_snip] if idle_snip else []),
                *(out or [Omitted(self.harness, f"no {role} groups for Claude Code")]),
            ]
        prov = knobs()["providers"]["claude-code"]
        pnote = prov.get(provider, prov["anthropic"]) if provider != "default" else prov["anthropic"]
        notes = [pnote, "Gateway or custom model IDs: " + prov["gateway"]]
        if scope in ("global", "project") and fmt == "json":
            frag = {"modelSettings": {m: {"autoCompactWindow": v} for m, v in values.items()}}
            path = "~/.claude/settings.json" if scope == "global" else ".claude/settings.local.json"
            notes.insert(0, "Needs Claude Code v2.1.288+. Merge into the existing file; do not replace it.")
            if scope == "project":
                notes.insert(1, "Use .claude/settings.json instead to share with your team (commit it).")
            idle, line = idle_note(result, profile) if scope == "global" else (None, "")
            if line and not (idle and idle["switch"]):
                notes.append(line)
            out.insert(0, Snippet(
                self.harness, scope, profile, fmt, f"Claude Code · {scope} · {profile}: per-model auto-compact window",
                TARGETS[scope], json.dumps(frag, indent=2), _merge_py(path, values), _revert_py(path, list(values)),
                ev, notes, [], [KN_MODEL]))  # fmt: skip
            if idle_snip:
                out.insert(1, idle_snip)
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

    def _idle_snippet(self, result: dict[str, Any], scope: str, profile: str, fmt: str) -> Snippet | None:
        """`idleCompaction` for the global settings file, only when flipping it wins by more than the tolerance."""
        if scope != "global" or fmt != "json":
            return None
        idle, line = idle_note(result, profile)
        if not (idle and idle["switch"]):
            return None
        want = not result["idle_compaction"]["enabled"]
        path = "~/.claude/settings.json"
        return Snippet(
            self.harness, scope, profile, fmt, f"Claude Code · idle compaction · {profile}", TARGETS[scope],
            json.dumps({"idleCompaction": want}, indent=2), _idle_py(path, want),
            _idle_py(path, None) + "   # back to the default (on)", [line],
            ["One setting for every model and project (a project's settings file can override it). "
             "Needs Claude Code v2.1.290+."], [], [KN_IDLE])  # fmt: skip


__all__ = ["ClaudeCodeRecommender", "k"]
