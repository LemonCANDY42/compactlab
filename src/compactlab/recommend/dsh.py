"""DeepSeek Harness recommender: `modelPolicies` entries for the dsh-compaction-basic plugin config.

dsh sets the threshold as a fraction of the window (`thresholdRatio`), capped by the output reserve plus
`headroomTokens`. The snippet sets `headroomTokens: 0` so the ratio alone decides, as long as the value stays
below window - reserve (the effective max). Where the user's plugin config lives is not verified, so the snippet
names the plugin and leaves placement to the user; `apply` does not write dsh config.
"""

from __future__ import annotations

from typing import Any, ClassVar

from . import Omitted, Recommender, Snippet, evidence, knobs, register, safe_value

KN = "dsh.plugin.compaction-basic.modelPolicies"
RETAIN_RATIO = 0.16  # plugin default; thresholdRatio must stay above it
PROVIDER = "deepseek-official"
SUMMARY_MAX = 65_536  # the plugin's default summary cap (= default headroomTokens)


def ratio_for(v: int, window: int) -> float:
    """thresholdRatio that triggers at or just below v tokens (rounded down to 3 decimals)."""
    return int(v / window * 1000) / 1000


@register
class DshRecommender(Recommender):
    harness = "dsh"
    formats: ClassVar[dict[str, tuple[str, ...]]] = {"global": ("yaml",), "project": ("yaml",), "agent": ("yaml",)}

    def generate(self, result: dict[str, Any], scope: str, profile: str, fmt: str | None, provider: str) -> list:
        fmt = fmt or "yaml"
        if fmt != "yaml":
            return [Omitted(self.harness, f"format {fmt} is not available; dsh plugin config is YAML")]
        if scope != "global":
            return [Omitted(self.harness, "dsh policies are per provider/model, not per project or agent role; "
                                          "use the global snippet")]  # fmt: skip
        out: list[Snippet | Omitted] = []
        rows = []
        notes: list[str] = []
        ev = []
        for g in sorted(result["groups"], key=lambda g: -g["n_turns"]):
            if g["harness"] != self.harness or g["role"] != "main":
                continue
            v, why = safe_value(g, profile, result, 1, 10**9)
            if v is None:
                out.append(Omitted(self.harness, why))
                continue
            r = ratio_for(v, int(g["max_window"]))
            if r <= RETAIN_RATIO:
                out.append(Omitted(self.harness, f"{g['model']}: ratio {r} is not above retainRatio {RETAIN_RATIO}"))
                continue
            rows.append(
                f"    - provider: {PROVIDER}\n      model: {g['model']}\n"
                f"      thresholdRatio: {r}   # ≈ {int(r * g['max_window']) // 1000}k of {g['max_window']:,}\n"
                f"      headroomTokens: 0\n"
                f"      maxTokens: {SUMMARY_MAX}   # summary cap; without it dsh derives 0 from headroomTokens"
            )
            ev.append(evidence(g, profile))
        if not rows:
            return out or [Omitted(self.harness, "no dsh main sessions in this history")]
        content = (
            "# under the @deepseek-ai/dsh-compaction-basic plugin's config\n"
            "config:\n  modelPolicies:\n" + "\n".join(rows)
        )
        notes += [
            f"provider is set to {PROVIDER}; change it to the provider id your route uses (see request/context in the "
            "session log).",
            "headroomTokens: 0 lets thresholdRatio decide; dsh still caps the trigger at window - reserved output.",
            "Subagents use the same policy for the same model; dsh has no separate subagent threshold.",
        ]
        warn = [knobs()["knobs"][KN]["note"]]
        out.insert(0, Snippet(self.harness, scope, profile, fmt, f"dsh · compaction-basic · {profile}",
                              "the compaction-basic plugin entry in your dsh profile / cordis.yml", content, "",
                              "delete the modelPolicies entries above", ev, notes, warn, [KN]))  # fmt: skip
        return out
