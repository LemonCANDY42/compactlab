"""Heuristic fact-checks for compaction summaries (offline; no LLM)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..model import Compaction, Session

CLAIM_RE = re.compile(
    r"(all )?(unit |integration )?tests? (now )?(pass|passed|passing|green)|"
    r"build (succeed|succeeded|passes|passed|is green)|"
    r"\b(verified|confirmed|validated)\b|\b(fixed|resolved)\b|lint(ing)? (clean|passes)",
    re.I,
)
RUN_RE = re.compile(
    r"(pytest|npm (run )?test|yarn test|cargo test|go test|make test|jest|vitest|xcodebuild|swift test|"
    r"tsc\b|npm run build|cargo build|make\b|ruff|eslint)",
    re.I,
)
FAIL_RE = re.compile(r"(FAILED|\bfailed\b|\berror\b|Traceback|AssertionError|\b[1-9]\d* failed)", re.I)
PATH_RE = re.compile(r"(?<![\w.])(/[\w.\-]+(?:/[\w.\-]+)+|[\w.\-]+(?:/[\w.\-]+)+\.\w{1,5})")
TICK_RE = re.compile(r"`([^`\n]{3,60})`")
LOOKBACK = 200


@dataclass
class Flag:
    kind: str  # unsupported_claim | contradicted_claim | unknown_path | unknown_identifier
    text: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {"kind": self.kind, "text": self.text, "detail": self.detail}


def _evidence(s: Session, c: Compaction) -> tuple[str, list[str]]:
    turns = s.turns[max(0, c.turn_index - LOOKBACK) : c.turn_index]
    blob = " ".join(" ".join(t.tool_keys) + " " + " ".join(t.snippets) + " " + t.user_text for t in turns)
    cmds = [sn for t in turns for sn in t.snippets if sn.startswith("$ ")]
    # include tool keys such as "Bash:pytest -q" or "shell:pytest" as commands
    cmds += [k for t in turns for k in t.tool_keys if ":" in k and RUN_RE.search(k)]
    return blob, cmds


def check_summary(s: Session, c: Compaction) -> list[Flag]:
    text = c.summary_text
    if not text:
        return []
    blob, cmds = _evidence(s, c)
    flags: list[Flag] = []
    ran = [x for x in cmds if RUN_RE.search(x)]
    last_failed = False
    if ran:
        # crude: look at result snippets after the last run command
        tail = " ".join(sn for t in s.turns[max(0, c.turn_index - 15) : c.turn_index] for sn in t.snippets)
        last_failed = bool(FAIL_RE.search(tail))
    for sent in re.split(r"(?<=[.;\n])\s+", text):
        if not CLAIM_RE.search(sent):
            continue
        if not ran:
            flags.append(
                Flag("unsupported_claim", sent.strip()[:160], "no test/build command found before the compaction")
            )
        elif last_failed:
            flags.append(Flag("contradicted_claim", sent.strip()[:160], "failure markers in recent tool output"))
    for m in sorted(set(PATH_RE.findall(text))):
        if m not in blob and m.split("/")[-1] not in blob:
            flags.append(Flag("unknown_path", m, "path never appears in pre-compaction tool calls or output"))
    for m in sorted(set(TICK_RE.findall(text))):
        if "/" in m or " " in m:
            continue
        if m not in blob:
            flags.append(Flag("unknown_identifier", m, "identifier never appears before the compaction"))
    return flags
