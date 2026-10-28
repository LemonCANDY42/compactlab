"""Codex recommender: config.toml (global / trusted project), -c flag, role layer (unverified)."""

from __future__ import annotations

from typing import Any, ClassVar

from . import Omitted, Recommender, Snippet, evidence, knobs, register, safe_value

KEY = "model_auto_compact_token_limit"
KN = "codex.config.model_auto_compact_token_limit"
KN_FLAG = "codex.flag.-c"
KN_AGENT = "codex.agent.model_auto_compact_token_limit"

MERGE = (
    'cp {p} {p}.bak 2>/dev/null; python3 -c "import os,re,sys;p=os.path.expanduser(sys.argv[1]);'
    "t=open(p).read() if os.path.exists(p) else '';m=re.search(r'^[ \\t]*\\[',t,re.M);i=m.start() if m else len(t);"
    "top,rest=t[:i],t[i:];line='{k} = '+sys.argv[2];"
    "top=re.sub(r'^[ \\t]*{k}[ \\t]*=.*$',line,top,flags=re.M) if re.search(r'^[ \\t]*{k}[ \\t]*=',top,re.M) "
    "else (top.rstrip('\\n')+'\\n' if top.strip() else '')+line+'\\n'+('\\n' if rest else '');"
    "open(p,'w').write(top+rest)\" {p} {v}"
)
REVERT = "remove the `{k} = ...` line above the first [table] in {p} (or restore {p}.bak)"


@register
class CodexRecommender(Recommender):
    harness = "codex"
    formats: ClassVar[dict[str, tuple[str, ...]]] = {
        "global": ("toml", "shell", "env"),
        "project": ("toml",),
        "agent": ("toml",),
    }

    def generate(self, result: dict[str, Any], scope: str, profile: str, fmt: str | None, provider: str) -> list:
        fmt = fmt or self.formats[scope][0]
        if fmt not in self.formats.get(scope, ()):
            return [Omitted(self.harness, f"format {fmt} is not available for scope {scope}; "
                                          f"use one of {', '.join(self.formats[scope])}")]  # fmt: skip
        if fmt == "env":
            return [Omitted(self.harness, knobs()["knobs"]["codex.env"]["note"] + " Use config.toml or -c instead.")]
        role = "subagent" if scope == "agent" else "main"
        groups = [g for g in result["groups"] if g["harness"] == self.harness and g["role"] == role]
        out: list[Snippet | Omitted] = []
        picks = []
        for g in sorted(groups, key=lambda g: -g["n_turns"]):
            v, why = safe_value(g, profile, result, 1, 10**9)
            if v is None:
                out.append(Omitted(self.harness, why))
            else:
                picks.append((g, v))
        if not picks:
            return out or [Omitted(self.harness, f"no Codex {role} sessions in this history")]
        g, v = picks[0]
        prov = knobs()["providers"]["codex"]
        notes = [
            "Codex has one global limit, not one per model: this value follows " + g["model"] + " (most turns).",
            prov.get(provider, prov["openai"]) if provider != "default" else prov["openai"],
        ]
        notes += [f"Also seen: {o['model']} would get {o_v // 1000}k." for o, o_v in picks[1:]]
        ev = [evidence(g, profile)]
        if scope in ("global", "project"):
            path = "~/.codex/config.toml" if scope == "global" else ".codex/config.toml"
            if scope == "project":
                notes.insert(0, "Project config is loaded only for trusted projects.")
            if fmt == "shell":
                out.insert(0, Snippet(self.harness, scope, profile, fmt, f"Codex · -c flag · {profile}", "command line",
                                      f"codex -c {KEY}={v}", "", "run without -c", ev,
                                      ["One run; highest precedence.", *notes], [], [KN_FLAG]))  # fmt: skip
            else:
                out.insert(0, Snippet(self.harness, scope, profile, fmt,
                                      f"Codex · {scope} · {profile}: auto-compact limit",
                                      path + " (top level, above any [table])", f"{KEY} = {v}",
                                      MERGE.format(p=path, k=KEY, v=v), REVERT.format(k=KEY, p=path),
                                      ev, notes, [], [KN]))  # fmt: skip
        else:
            content = (f"# ~/.codex/config.toml\n[agents.<role>]\nconfig_file = \"<role>.toml\"\n\n"
                       f"# <role>.toml (config layer for that role)\n{KEY} = {v}")  # fmt: skip
            out.insert(0, Snippet(self.harness, scope, profile, fmt, f"Codex · agent role · {profile}",
                                  "~/.codex/config.toml + <role>.toml", content, "", "remove the key from <role>.toml",
                                  ev, notes, [knobs()["knobs"][KN_AGENT]["note"]], [KN_AGENT]))  # fmt: skip
        return out
